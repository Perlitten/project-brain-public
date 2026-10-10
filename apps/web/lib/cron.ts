// Turns the common five-field cron shapes into a sentence ("Daily at 00:30 UTC").
// Anything unusual falls back to the raw expression, which the UI still shows on hover.

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const num = (s: string) => (/^\d+$/.test(s) ? Number(s) : null);
const pad = (n: number) => String(n).padStart(2, "0");

export function cronToText(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [min, hour, dom, mon, dow] = parts;
  const m = num(min);
  const h = num(hour);
  const everyMin = /^\*\/(\d+)$/.exec(min);
  const everyHour = /^\*\/(\d+)$/.exec(hour);
  if (dom !== "*" || mon !== "*") return cron;

  if (dow === "*") {
    if (everyMin && hour === "*") return `Every ${everyMin[1]} minutes`;
    if (m !== null && hour === "*") return m === 0 ? "Every hour" : `Every hour at :${pad(m)}`;
    if (m !== null && everyHour) return `Every ${everyHour[1]} hours`;
    if (m !== null && h !== null) return `Daily at ${pad(h)}:${pad(m)} UTC`;
    return cron;
  }
  const d = num(dow);
  if (d !== null && d <= 7 && m !== null && h !== null) return `${DAYS[d % 7]}s at ${pad(h)}:${pad(m)} UTC`;
  return cron;
}
