import { demoApi } from "./demo";
import { ApiError } from "./errors";
import type {
  IncidentPage,
  IncidentQuery,
  Overview,
  ServiceDetail,
  ServiceSummary,
} from "./types";

export { ApiError };

export interface CloudPulseApi {
  readonly demo: boolean;
  overview(signal?: AbortSignal): Promise<Overview>;
  services(signal?: AbortSignal): Promise<ServiceSummary[]>;
  service(name: string, signal?: AbortSignal): Promise<ServiceDetail>;
  incidents(query: IncidentQuery, signal?: AbortSignal): Promise<IncidentPage>;
}

const REQUEST_TIMEOUT_MS = 10_000;

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

export function createHttpApi(baseUrl = "", fetchImpl: typeof fetch = fetch): CloudPulseApi {
  async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const onAbort = () => controller.abort();
    signal?.addEventListener("abort", onAbort);
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        headers: { Accept: "application/json" },
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
          ? "The CloudPulse API did not respond in time."
          : "Cannot reach the CloudPulse API. Is `cloudpulse serve` running?",
      );
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    }

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (!response.ok) {
      const data = (body ?? {}) as { error?: string; message?: string };
      throw new ApiError(
        response.status,
        data.error ?? "http_error",
        data.message ?? `Request failed with HTTP ${response.status}.`,
      );
    }
    if (body === null) {
      throw new ApiError(response.status, "invalid_response", "The API returned invalid JSON.");
    }
    return body as T;
  }

  return {
    demo: false,
    overview: (signal) => get<Overview>("/api/overview", signal),
    services: async (signal) =>
      (await get<{ items: ServiceSummary[] }>("/api/services", signal)).items,
    service: (name, signal) =>
      get<ServiceDetail>(`/api/services/${encodeURIComponent(name)}`, signal),
    incidents: (query, signal) =>
      get<IncidentPage>(`/api/incidents${buildQuery(query)}`, signal),
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
