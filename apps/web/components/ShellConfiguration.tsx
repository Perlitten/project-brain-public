"use client";
import { createContext, useContext, useEffect } from "react";
import type { ShellConfiguration } from "./Shell";
export const ShellConfigurationContext = createContext<(configuration: ShellConfiguration) => void>(() => {});
export const ShellUrgentContext = createContext<(urgent: number) => void>(() => {});

/** The server streams configuration once it is authorized and loaded. The
 * shell can show navigation/headings meanwhile; actions stay locked initially.
 */
export function ApplyShellConfiguration({ value }: { value: ShellConfiguration }) {
  const apply = useContext(ShellConfigurationContext);
  useEffect(() => { apply(value); }, [apply, value]);
  return null;
}

/** The inbox count can require many independent reads. Stream it after the
 * shell is configured so a slow Board never keeps navigation or actions stale. */
export function ApplyShellUrgent({ value }: { value: number }) {
  const apply = useContext(ShellUrgentContext);
  useEffect(() => { apply(value); }, [apply, value]);
  return null;
}
