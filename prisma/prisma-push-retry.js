// scripts/prisma-push-retry.js
// ESM compatibile (dato "type":"module" nel package.json)

import { execSync } from "node:child_process";

const MAX_TRIES = 30;      // ~2.5 min
const SLEEP_MS  = 5000;

for (let i = 1; i <= MAX_TRIES; i++) {
  try {
    console.log(`[prisma] Tentativo ${i}/${MAX_TRIES} → db push`);
    execSync("npx prisma db push", { stdio: "inherit" });
    console.log("[prisma] OK");
    process.exit(0);
  } catch (e) {
    console.log(`[prisma] DB non pronto (P1001?). Riprovo tra ${SLEEP_MS/1000}s…`);
    await new Promise((r) => setTimeout(r, SLEEP_MS));
  }
}

console.error("[prisma] Fallito dopo molti tentativi.");
process.exit(1);
