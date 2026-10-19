import { createContext, useContext, type ReactNode } from "react";
import type { CloudPulseApi } from "./client";

const ApiContext = createContext<CloudPulseApi | null>(null);

export function ApiProvider({ api, children }: { api: CloudPulseApi; children: ReactNode }) {
  return <ApiContext.Provider value={api}>{children}</ApiContext.Provider>;
}

export function useApi(): CloudPulseApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error("useApi must be used inside <ApiProvider>.");
  return api;
}
