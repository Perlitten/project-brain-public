// How each sign-in mode reads: the chip on Access → Sign-in and its view tab badge.
// A plain module so the server page can read it too.
import type { SignInView } from "@/lib/sign-in-view";

export const SIGN_IN_STATUS: Record<SignInView["mode"], { tone: "ok" | "warn" | "info" | "idle" | "bad"; chip: string; title: string }> = {
  open: { tone: "warn", chip: "No password", title: "Open mode: anyone who opens this address gets in" },
  password: { tone: "ok", chip: "Password set", title: "Only people with the password get in" },
  env: { tone: "info", chip: "Set by the server", title: "Sign-in is fixed by the deployment" },
  demo: { tone: "idle", chip: "Demo data", title: "Nothing to protect yet" },
  team: { tone: "ok", chip: "Team members", title: "Every member signs in with their own key" },
  unknown: { tone: "bad", chip: "Can’t check", title: "Brain isn’t answering" },
};
