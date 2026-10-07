import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import { Suspense } from "react";
import { BOOT_SCRIPT, Boot } from "@/components/Boot";
import { Shell, type ShellRepo } from "@/components/Shell";
import { actionsMode } from "@/lib/actions/gate";
import { dataSource, getRepositories } from "@/lib/data";
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

export const metadata: Metadata = {
  title: { default: "Project Brain", template: "%s · Project Brain" },
  description: "See what your AI agents know about your code, and whether it is up to date.",
};

export const viewport: Viewport = { themeColor: "#05080B", colorScheme: "dark" };

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const repos: ShellRepo[] = (await getRepositories()).map((r) => ({
    slug: r.slug,
    name: r.name,
    path: r.path,
    ...repositoryStatus(r),
  }));
  const live = dataSource !== "demo";
  const first = repos[0];
  const bootLines = [
    "› waking up project brain",
    live ? `› memory online · ${repos.length} repositor${repos.length === 1 ? "y" : "ies"}` : "› demo data · api not connected",
    first ? `› ${first.name} · ${first.status.toLowerCase()}` : "› no repositories yet",
    "› ready",
  ];
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: BOOT_SCRIPT }} />
      </head>
      <body>
        <Boot lines={bootLines} />
        <Suspense>
          <Shell repos={repos} demo={dataSource === "demo"} mode={actionsMode()}>
            {children}
          </Shell>
        </Suspense>
      </body>
    </html>
  );
}
