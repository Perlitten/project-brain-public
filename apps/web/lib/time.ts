/** All server observations use an explicit timezone, independent of browser locale. */
export function utcTime(value: string | null | undefined): string {
  if (!value) return "unknown time";
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return `${value} (timezone unknown)`;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "unknown time";
  return date.toLocaleString("en-GB", { timeZone: "UTC", year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }) + " UTC";
}

export function setupDetail(value: string): string {
  return value.replace(/\bllm:/gi, "Language model:").replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})/g, utcTime);
}
