import type { SettingsSection } from "./settings-view";

export function telegramConfigured(section?: SettingsSection): boolean {
  const token = section?.fields.find((f) => f.key === "TELEGRAM_ALERT_BOT_TOKEN");
  const chat = section?.fields.find((f) => f.key === "TELEGRAM_ALERT_CHAT_ID");
  return token?.set === true && typeof chat?.value === "string" && chat.value.trim().length > 0;
}
