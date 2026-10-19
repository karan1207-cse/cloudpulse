// Types mirror the JSON returned by src/cloudpulse/api.py.

export type Severity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type IncidentStatus = "OPEN" | "RECOVERED";
export type ServiceStatus = "healthy" | "unhealthy" | "unknown";

export const SEVERITIES: Severity[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
export const INCIDENT_STATUSES: IncidentStatus[] = ["OPEN", "RECOVERED"];

export interface HealthCheck {
  id: string;
  service: string;
  url: string;
  final_url?: string;
  checked_at: string;
  is_healthy: boolean;
  status_code: number | null;
  response_time_ms: number;
  error: string | null;
  error_type: string | null;
  redirects?: number;
}

export interface Incident {
  id: string;
  service_name: string;
  service_url: string;
  severity: Severity;
  status: IncidentStatus;
  created_at: string;
  updated_at: string | null;
  recovered_at: string | null;
  duration_seconds: number | null;
  failure_count: number;
  last_error: string | null;
  last_error_type: string | null;
  last_status_code: number | null;
  escalated_at?: string | null;
}

export interface ServiceSummary {
  name: string;
  url: string;
  timeout_seconds: number;
  failure_threshold: number;
  severity: Severity;
  accepted_status: [number, number];
  enabled: boolean;
  status: ServiceStatus;
  last_checked_at: string | null;
  last_status_code: number | null;
  last_response_time_ms: number | null;
  last_error: string | null;
  last_error_type: string | null;
  consecutive_failures: number;
  current_incident: { id: string; severity: Severity; created_at: string } | null;
  /** "config" = defined in the services file (read-only here); "dashboard" = added here. */
  source?: "config" | "dashboard";
  editable?: boolean;
  escalation_threshold?: number | null;
  check_mode?: CheckMode;
  backend_timeout_seconds?: number;
}

export type CheckMode = "quick" | "full";
export type ComponentKind = "page" | "asset" | "backend";
export type ComponentStatus = "up" | "down" | "skipped" | "blocked" | "disabled" | "pending";

export interface SiteComponent {
  url: string;
  kind: ComponentKind;
  /** main = the service URL; html = found in the page; discovered = found in JS; manual = added by you */
  source: "main" | "html" | "discovered" | "manual";
  role?: "main";
  status: ComponentStatus;
  status_code: number | null;
  response_time_ms: number | null;
  error: string | null;
  error_type: string | null;
}

export interface SiteView {
  check_mode: CheckMode;
  discovered_at: string | null;
  last_checked_at: string | null;
  components: SiteComponent[];
  limits: { max_manual: number; max_backends: number; max_pages: number };
}

/** What the form sends to POST/PUT /api/services. */
export interface ServiceInput {
  name: string;
  url: string;
  timeout_seconds: number;
  failure_threshold: number;
  severity: Severity;
  accepted_status: [number, number];
  enabled: boolean;
  check_mode: CheckMode;
  backend_timeout_seconds: number;
}

export interface SaveResult {
  service: ServiceSummary;
  warnings: string[];
}

export interface CheckOutcome {
  service: string;
  skipped: boolean;
  reason: string | null;
  transition: string;
  is_healthy: boolean | null;
  status_code: number | null;
  response_time_ms: number | null;
  error_type: string | null;
  error?: string | null;
  check_mode?: CheckMode | null;
}

export interface CheckResult {
  outcome: CheckOutcome | null;
  failure: { service: string; error: string } | null;
  service: ServiceSummary;
}

export interface CheckAllResult {
  checked: number;
  healthy: number;
  unhealthy: number;
  failures: { service: string; error: string }[];
}

export interface SchedulerStatus {
  enabled: boolean;
  interval_seconds?: number;
  active_here?: boolean;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_error?: string | null;
}

export interface Health {
  status: string;
  backend: string;
  can_write: boolean;
  can_check: boolean;
  auth_required: boolean;
  service_store: boolean;
  scheduler: SchedulerStatus;
  limits: { max_services: number; services: number };
  defaults: {
    timeout_seconds: number;
    failure_threshold: number;
    severity: Severity;
    accepted_status: [number, number];
  };
}

export interface Overview {
  generated_at: string;
  totals: {
    services: number;
    healthy: number;
    unhealthy: number;
    unknown: number;
    open_incidents: number;
  };
  open_incidents: Incident[];
  recent_recoveries: Incident[];
  recent_checks: HealthCheck[];
}

export interface ServiceDetail {
  service: ServiceSummary;
  checks: HealthCheck[];
  incidents: Incident[];
  site?: SiteView;
}

export interface IncidentPage {
  items: Incident[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  truncated: boolean;
}

export type SortField = "created_at" | "recovered_at" | "severity" | "service_name" | "status";

export interface IncidentQuery {
  status?: IncidentStatus;
  severity?: Severity;
  service?: string;
  sort?: SortField;
  order?: "asc" | "desc";
  page?: number;
  page_size?: number;
}
