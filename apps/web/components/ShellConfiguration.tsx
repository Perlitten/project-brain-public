"use client";
import { createContext, useContext, useEffect } from "react";
import type { ShellConfiguration } from "./Shell";
export const ShellConfigurationContext = createContext<(configuration: ShellConfiguration) => void>(() => {});

/** The server streams configuration once it is authorized and loaded. The
 * shell can show navigation/headings meanwhile; actions stay locked initially.
 */
export function ApplyShellConfiguration({ value }: { value: ShellConfiguration }) {
  const apply = useContext(ShellConfigurationContext);
  useEffect(() => { apply(value); }, [apply, value]);
  return null;
}
