import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import { Suspense } from "react";
import { Shell, type ShellRepo } from "@/components/Shell";
import { dataSource, getRepositories } from "@/lib/data";
import "./globals.css";

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
    tone: r.behind > 20 ? "bad" : r.behind > 0 ? "warn" : "ok",
    status: r.behind ? `${r.behind} changes not read yet` : "Up to date",
  }));
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body>
        <Suspense>
          <Shell repos={repos} demo={dataSource === "demo"}>
            {children}
          </Shell>
        </Suspense>
      </body>
    </html>
  );
}
