import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import { Suspense } from "react";
import { connection } from "next/server";
import { BOOT_SCRIPT, Boot } from "@/components/Boot";
import { getCapabilities } from "@/lib/capabilities";
import { ApplyShellConfiguration } from "@/components/ShellConfiguration";
import { Shell, type ShellRepo } from "@/components/Shell";
import { actionsMode } from "@/lib/actions/gate";
import { signInMode } from "@/lib/sign-in-view";
import { currentMember } from "@/lib/team-session";
import { dataSource, getRepositoryFeed } from "@/lib/data";
import { repositoryStatus } from "@/lib/repository-status";
import "./globals.css";
import "./actions.css";

const sans = localFont({
  src: [
    { path: "../public/fonts/Outfit-400-latin.woff2", weight: "400" },
    { path: "../public/fonts/Outfit-500-latin.woff2", weight: "500" },
    { path: "../public/fonts/Outfit-600-latin.woff2", weight: "600" },
  ],
  variable: "--font-sans",
  display: "swap",
});

const mono = localFont({
  src: [
    { path: "../public/fonts/JetBrainsMono-400-latin.woff2", weight: "400" },
    { path: "../public/fonts/JetBrainsMono-500-latin.woff2", weight: "500" },
  ],
  variable: "--font-mono",
  display: "swap",
});

const DESCRIPTION = "See what your AI agents know about your code, and whether it is up to date.";

// A live dashboard (any Brain API setting present) is private: never indexed.
// Pure demo data may be. Same rule as app/robots.ts.
export async function generateMetadata(): Promise<Metadata> {
  const demo = !process.env.BRAIN_API_URL?.trim() && !process.env.BRAIN_API_KEY?.trim();
  return {
    title: { default: "Project Brain", template: "%s · Project Brain" },
    description: DESCRIPTION,
    robots: demo ? { index: true, follow: true } : { index: false, follow: false },
    openGraph: { title: "Project Brain", description: DESCRIPTION, type: "website", siteName: "Project Brain" },
  };
}

export const viewport: Viewport = { themeColor: "#05080B", colorScheme: "dark" };

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // API bindings are runtime configuration (including pre-built containers).
  // Do not persist a build-time demo shell as the live dashboard's HTML.
  await connection();
  const bootLines = ["› opening Project Brain", dataSource === "demo" ? "› synthetic demo" : "› connecting to your Brain server", "› loading workspace"];
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: BOOT_SCRIPT }} />
      </head>
      <body>
        <Boot lines={bootLines} />
        <Suspense>
          <Shell demo={dataSource === "demo"}>
            <Suspense fallback={null}><Configuration /></Suspense>
            {children}
          </Shell>
        </Suspense>
      </body>
    </html>
  );
}


async function Configuration() {
  const [feed, mode, access, member, capabilities] = await Promise.all([getRepositoryFeed(), actionsMode(), signInMode(), currentMember(), getCapabilities()]);
  const repos: ShellRepo[] = feed.repositories.map((r) => ({
    slug: r.slug,
    name: r.name,
    path: r.path,
    ...repositoryStatus(r),
  }));
  return <ApplyShellConfiguration value={{ repos, mode, access, capabilities, member: member ? { id: member.id, name: member.name, admin: member.admin } : null }} />;
}
