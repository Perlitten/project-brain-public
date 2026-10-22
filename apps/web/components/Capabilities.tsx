"use client";
import { createContext, useContext, type ReactNode } from "react";
import type { Capabilities, Capability } from "@/lib/capability-contract";

const Context = createContext<Capabilities>({ state: "unavailable", supported: [] });
export const useCapabilities = () => useContext(Context);
export function CapabilitiesProvider({ value, children }: { value: Capabilities; children: ReactNode }) {
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function useCapability(name: Capability): boolean { return useCapabilities().supported.includes(name); }
