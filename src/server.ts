import express from 'express';
import cors from 'cors';
import { z } from 'zod';
import { PrismaClient } from '@prisma/client';
import path from 'path';


const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(process.cwd(), 'public')));


const prisma = new PrismaClient();


const SubscriberSchema = z.object({
phone: z.string().min(6),
discord_nick: z.string().optional(),
telegram_nick: z.string().optional(),
bitget_uid: z.string().optional(),
email: z.string().email(),
plan: z.enum(['MONTHLY','YEARLY'])
});


app.get('/health', (_req, res) => res.json({ ok: true }));


app.post('/api/subscribe', async (req, res) => {
try {
const data = SubscriberSchema.parse(req.body);


const start = new Date();
const end = new Date(start);
if (data.plan === 'MONTHLY') end.setMonth(end.getMonth() + 1);
else end.setFullYear(end.getFullYear() + 1);


const subscriber = await prisma.subscriber.create({
data: {
phone: data.phone,
discord_nick: data.discord_nick,
telegram_nick: data.telegram_nick,
bitget_uid: data.bitget_uid,
email: data.email,
subscriptions: {
create: { plan: data.plan as any, start_date: start, end_date: end }
}
}
});


res.status(201).json({ id: subscriber.id });
} catch (e: any) {
res.status(400).json({ error: e.message });
}
});


const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`Server running on :${port}`));