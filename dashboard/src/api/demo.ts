// DEMO DATA — fabricated sample values used only when demo mode is enabled
// explicitly (?demo=1 or VITE_DEMO_MODE=true). The UI shows a persistent
// banner whenever this module is in use. These are NOT real measurements.
import type { CloudPulseApi } from "./client";
import { ApiError } from "./errors";
import type {
  Health,
  HealthCheck,
  Incident,
  IncidentPage,
  Overview,
  ServiceDetail,
  ServiceSummary,
  SiteView,
} from "./types";
import { SEVERITIES } from "./types";

const BASE = Date.parse("2026-01-15T12:00:00Z");
const minutes = (n: number) => new Date(BASE - n * 60_000).toISOString();

const services: ServiceSummary[] = [
  {
    name: "demo-storefront",
    url: "https://storefront.example.com/health",
    timeout_seconds: 5,
    failure_threshold: 3,
    severity: "HIGH",
    accepted_status: [200, 399],
    enabled: true,
    status: "healthy",
    last_checked_at: minutes(1),
    last_status_code: 200,
    last_response_time_ms: 182.4,
    last_error: null,
    last_error_type: null,
    consecutive_failures: 0,
    current_incident: null,
  },
  {
    name: "demo-payments",
    url: "https://payments.example.com/status",
    timeout_seconds: 5,
    failure_threshold: 2,
    severity: "CRITICAL",
    accepted_status: [200, 299],
    enabled: true,
    status: "unhealthy",
    last_checked_at: minutes(1),
    last_status_code: 503,
    last_response_time_ms: 912.7,
    last_error: "HTTP error: 503",
    last_error_type: "HTTP_STATUS",
    consecutive_failures: 4,
    current_incident: { id: "demo-inc-3", severity: "CRITICAL", created_at: minutes(3) },
  },
  {
    name: "demo-search",
    url: "https://search.example.com/",
    timeout_seconds: 3,
    failure_threshold: 3,
    severity: "MEDIUM",
    accepted_status: [200, 399],
    enabled: true,
    status: "unknown",
    last_checked_at: null,
    last_status_code: null,
    last_response_time_ms: null,
    last_error: null,
    last_error_type: null,
    consecutive_failures: 0,
    current_incident: null,
  },
];

function checksFor(service: ServiceSummary, count: number): HealthCheck[] {
  if (service.last_checked_at === null) return [];
  return Array.from({ length: count }, (_, i) => {
    const failing = service.status === "unhealthy" && i < service.consecutive_failures;
    return {
      id: `${service.name}-check-${i}`,
      service: service.name,
      url: service.url,
      checked_at: minutes(1 + i),
      is_healthy: !failing,
      status_code: failing ? 503 : 200,
      response_time_ms: Math.round((150 + ((i * 37) % 90) + (failing ? 700 : 0)) * 10) / 10,
      error: failing ? "HTTP error: 503" : null,
      error_type: failing ? "HTTP_STATUS" : null,
    };
  });
}

const incidents: Incident[] = [
  {
    id: "demo-inc-3",
    service_name: "demo-payments",
    service_url: "https://payments.example.com/status",
    severity: "CRITICAL",
    status: "OPEN",
    created_at: minutes(3),
    updated_at: minutes(1),
    recovered_at: null,
    duration_seconds: null,
    failure_count: 4,
    last_error: "HTTP error: 503",
    last_error_type: "HTTP_STATUS",
    last_status_code: 503,
  },
  {
    id: "demo-inc-2",
    service_name: "demo-storefront",
    service_url: "https://storefront.example.com/health",
    severity: "HIGH",
    status: "RECOVERED",
    created_at: minutes(240),
    updated_at: minutes(215),
    recovered_at: minutes(215),
    duration_seconds: 1500,
    failure_count: 6,
    last_error: "Request timed out.",
    last_error_type: "TIMEOUT",
    last_status_code: null,
  },
  {
    id: "demo-inc-1",
    service_name: "demo-payments",
    service_url: "https://payments.example.com/status",
    severity: "MEDIUM",
    status: "RECOVERED",
    created_at: minutes(1440),
    updated_at: minutes(1430),
    recovered_at: minutes(1430),
    duration_seconds: 600,
    failure_count: 2,
    last_error: "DNS resolution failed",
    last_error_type: "DNS_ERROR",
    last_status_code: null,
  },
];

function demoSite(service: ServiceSummary): SiteView {
  const failing = service.status === "unhealthy";
  const origin = new URL(service.url).origin;
  return {
    check_mode: "full",
    discovered_at: minutes(30),
    last_checked_at: service.last_checked_at,
    limits: { max_manual: 10, max_backends: 5, max_pages: 10 },
    components: [
      { url: `${origin}/`, kind: "page", source: "main", role: "main", status: "up", status_code: 200, response_time_ms: 182, error: null, error_type: null },
      { url: `${origin}/assets/index-demo.js`, kind: "asset", source: "html", status: "up", status_code: 200, response_time_ms: 95, error: null, error_type: null },
      {
        url: "https://demo-api.example.com/",
        kind: "backend",
        source: "discovered",
        status: failing ? "down" : "up",
        status_code: failing ? null : 404,
        response_time_ms: failing ? 20000 : 310,
        error: failing ? "Request timed out." : null,
        error_type: failing ? "TIMEOUT" : null,
      },
    ],
  };
}

const readOnly = () =>
  Promise.reject(
    new ApiError(
      403,
      "demo_read_only",
      "Demo mode shows sample data only. Open the dashboard without ?demo=1 to add and check your own URLs.",
    ),
  );

export const demoApi: CloudPulseApi = {
  demo: true,
  async health(): Promise<Health> {
    return {
      status: "ok",
      backend: "demo",
      can_write: false,
      can_check: false,
      auth_required: false,
      service_store: false,
      scheduler: { enabled: false },
      limits: { max_services: 50, services: services.length },
      defaults: { timeout_seconds: 5, failure_threshold: 3, severity: "HIGH", accepted_status: [200, 399] },
    };
  },
  createService: readOnly,
  updateService: readOnly,
  deleteService: readOnly,
  checkService: readOnly,
  checkAll: readOnly,
  addComponent: readOnly,
  componentAction: readOnly,
  async overview(): Promise<Overview> {
    return {
      generated_at: new Date(BASE).toISOString(),
      totals: {
        services: services.length,
        healthy: services.filter((s) => s.status === "healthy").length,
        unhealthy: services.filter((s) => s.status === "unhealthy").length,
        unknown: services.filter((s) => s.status === "unknown").length,
        open_incidents: incidents.filter((i) => i.status === "OPEN").length,
      },
      open_incidents: incidents.filter((i) => i.status === "OPEN"),
      recent_recoveries: incidents.filter((i) => i.status === "RECOVERED"),
      recent_checks: services
        .flatMap((s) => checksFor(s, 3))
        .sort((a, b) => b.checked_at.localeCompare(a.checked_at))
        .slice(0, 10),
    };
  },
  async services() {
    return services;
  },
  async service(name): Promise<ServiceDetail> {
    const service = services.find((s) => s.name === name);
    if (!service) throw new ApiError(404, "not_found", `Service '${name}' is not configured.`);
    return {
      service,
      checks: checksFor(service, 40),
      incidents: incidents.filter((i) => i.service_name === name),
      site: demoSite(service),
    };
  },
  async incidents(query): Promise<IncidentPage> {
    let items = incidents.filter(
      (i) =>
        (!query.status || i.status === query.status) &&
        (!query.severity || i.severity === query.severity) &&
        (!query.service || i.service_name === query.service),
    );
    const sort = query.sort ?? "created_at";
    const direction = query.order === "asc" ? 1 : -1;
    items = [...items].sort((a, b) => {
      if (sort === "severity") {
        return (SEVERITIES.indexOf(a.severity) - SEVERITIES.indexOf(b.severity)) * direction;
      }
      return String(a[sort] ?? "").localeCompare(String(b[sort] ?? "")) * direction;
    });
    const pageSize = query.page_size ?? 20;
    const page = query.page ?? 1;
    return {
      items: items.slice((page - 1) * pageSize, page * pageSize),
      page,
      page_size: pageSize,
      total: items.length,
      total_pages: Math.max(1, Math.ceil(items.length / pageSize)),
      truncated: false,
    };
  },
};
