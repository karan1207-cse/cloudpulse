import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useApi } from "./api/context";
import type { Health } from "./api/types";

type Tone = "success" | "error" | "info";

interface Toast {
  id: number;
  message: string;
  tone: Tone;
}

interface AppState {
  /** Server capabilities; null while loading or if the server is unreachable. */
  health: Health | null;
  refreshHealth: () => void;
  notify: (message: string, tone?: Tone) => void;
}

const AppStateContext = createContext<AppState | null>(null);
const TOAST_MS = 6000;

export function AppStateProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const [health, setHealth] = useState<Health | null>(null);
  const [nonce, setNonce] = useState(0);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  useEffect(() => {
    const controller = new AbortController();
    api.health(controller.signal).then(
      (data) => !controller.signal.aborted && setHealth(data),
      () => !controller.signal.aborted && setHealth(null),
    );
    return () => controller.abort();
  }, [api, nonce]);

  const refreshHealth = useCallback(() => setNonce((n) => n + 1), []);

  const dismiss = useCallback((id: number) => {
    setToasts((items) => items.filter((t) => t.id !== id));
  }, []);

  const notify = useCallback(
    (message: string, tone: Tone = "success") => {
      const id = nextId.current++;
      setToasts((items) => [...items.slice(-3), { id, message, tone }]);
      window.setTimeout(() => dismiss(id), TOAST_MS);
    },
    [dismiss],
  );

  return (
    <AppStateContext.Provider value={{ health, refreshHealth, notify }}>
      {children}
      <div className="toasts" aria-live="polite" aria-label="Notifications">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast-${toast.tone}`}>
            <span>{toast.message}</span>
            <button
              type="button"
              className="toast-close"
              aria-label="Dismiss notification"
              onClick={() => dismiss(toast.id)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </AppStateContext.Provider>
  );
}

export function useAppState(): AppState {
  const state = useContext(AppStateContext);
  if (!state) throw new Error("useAppState must be used inside <AppStateProvider>.");
  return state;
}
