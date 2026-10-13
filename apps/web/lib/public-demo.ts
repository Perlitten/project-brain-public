/** A public synthetic deployment must never carry operator connections. */
export function publicDemoProblem(env = process.env): string | null {
  if (!/^(1|true|yes|on)$/i.test(env.BRAIN_DEMO_MODE?.trim() ?? "")) return null;
  const forbidden = ["BRAIN_API_URL", "BRAIN_API_KEY", "WEB_BASIC_AUTH"] as const;
  if (forbidden.some((name) => Boolean(env[name]?.trim()))) {
    return "Public demo mode requires BRAIN_API_URL, BRAIN_API_KEY and WEB_BASIC_AUTH to be unset. Keep operator credentials in a separate protected deployment.";
  }
  return null;
}
