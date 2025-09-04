import { PrismaClient } from '@prisma/client';
import { freezeAccess } from './shared/access.js';
import { alert } from './shared/telegram.js';


const prisma = new PrismaClient();


async function run() {
const today = new Date();
const expiring = await prisma.subscription.findMany({
where: { status: 'ACTIVE', end_date: { lt: today } },
include: { subscriber: true }
});


for (const sub of expiring) {
await prisma.subscription.update({ where: { id: sub.id }, data: { status: 'EXPIRED' } });
await freezeAccess(sub.subscriber);
}


await alert(`Controllo scadenze completato. Scaduti gestiti: ${expiring.length}`);
}


run().then(() => process.exit(0)).catch((e) => { console.error(e); process.exit(1); });