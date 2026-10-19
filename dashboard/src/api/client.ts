import { getAdminToken } from "./auth";
import { demoApi } from "./demo";
import { ApiError } from "./errors";
import type {
  CheckAllResult,
  CheckResult,
  Health,
  IncidentPage,
  IncidentQuery,
  Overview,
  SaveResult,
  ServiceDetail,
  ServiceInput,
  ServiceSummary,
} from "./types";

export { ApiError };

export interface CloudPulseApi {
  readonly demo: boolean;
  health(signal?: AbortSignal): Promise<Health>;
  overview(signal?: AbortSignal): Promise<Overview>;
  services(signal?: AbortSignal): Promise<ServiceSummary[]>;
  service(name: string, signal?: AbortSignal): Promise<ServiceDetail>;
  incidents(query: IncidentQuery, signal?: AbortSignal): Promise<IncidentPage>;
  createService(input: ServiceInput): Promise<SaveResult>;
  updateService(name: string, input: ServiceInput): Promise<SaveResult>;
  deleteService(name: string): Promise<void>;
  checkService(name: string): Promise<CheckResult>;
  checkAll(): Promise<CheckAllResult>;
}

const REQUEST_TIMEOUT_MS = 10_000;
// "Check all" can take a while (each service may use its full timeout).
const CHECK_TIMEOUT_MS = 5 * 60_000;

function buildQuery(query: object): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export function createHttpApi(baseUrl = "", fetchImpl: typeof fetch = fetch): CloudPulseApi {
  async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const { method = "GET", body, signal, timeoutMs = REQUEST_TIMEOUT_MS } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const onAbort = () => controller.abort();
    signal?.addEventListener("abort", onAbort);
    const headers: Record<string, string> = { Accept: "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      const token = getAdminToken();
      if (token) headers.Authorization = `Bearer ${token}`;
    }
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method,
        headers,
        body: method === "GET" ? undefined : JSON.stringify(body ?? {}),
        signal: controller.signal,
        credentials: "same-origin",
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      const timedOut = controller.signal.aborted;
      throw new ApiError(
        0,
        timedOut ? "timeout" : "network_error",
        timedOut
          ? "The CloudPulse server did not respond in time."
          : "Cannot reach the CloudPulse server. Is `cloudpulse serve` running?",
      );
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    }

    let data: unknown = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) {
      const error = (data ?? {}) as { error?: string; message?: string; field?: string };
      throw new ApiError(
        response.status,
        error.error ?? "http_error",
        error.message ?? `Request failed with HTTP ${response.status}.`,
        error.field,
      );
    }
    if (data === null) {
      throw new ApiError(response.status, "invalid_response", "The API returned invalid JSON.");
    }
    return data as T;
  }

  const servicePath = (name: string) => `/api/services/${encodeURIComponent(name)}`;

  return {
    demo: false,
    health: (signal) => request<Health>("/api/health", { signal }),
    overview: (signal) => request<Overview>("/api/overview", { signal }),
    services: async (signal) =>
      (await request<{ items: ServiceSummary[] }>("/api/services", { signal })).items,
    service: (name, signal) => request<ServiceDetail>(servicePath(name), { signal }),
    incidents: (query, signal) =>
      request<IncidentPage>(`/api/incidents${buildQuery(query)}`, { signal }),
    createService: (input) =>
      request<SaveResult>("/api/services", { method: "POST", body: input }),
    updateService: (name, input) =>
      request<SaveResult>(servicePath(name), { method: "PUT", body: input }),
    deleteService: async (name) => {
      await request(servicePath(name), { method: "DELETE" });
    },
    checkService: (name) =>
      request<CheckResult>(`${servicePath(name)}/check`, {
        method: "POST",
        timeoutMs: CHECK_TIMEOUT_MS,
      }),
    checkAll: () =>
      request<CheckAllResult>("/api/checks", { method: "POST", timeoutMs: CHECK_TIMEOUT_MS }),
  };
}

/** Demo mode is opt-in: VITE_DEMO_MODE=true at build time or ?demo=1 in the URL. */
export function isDemoMode(search: string = window.location.search): boolean {
  if (import.meta.env.VITE_DEMO_MODE === "true") return true;
  return new URLSearchParams(search).get("demo") === "1";
}

export function createApi(): CloudPulseApi {
  if (isDemoMode()) return demoApi;
  return createHttpApi(import.meta.env.VITE_API_BASE_URL ?? "");
}
